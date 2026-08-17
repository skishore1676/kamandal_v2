---
title: Management quote coverage follows owned positions, not entry discovery
type: bug
area: unified lifecycle management
date: 2026-08-17
tags: [options, lifecycle, quotes, migration]
refs: [commit:9ea9f1e, src/kamandal_v2/strategy_lanes/management_runtime.py, tests/test_live_management_quote_refresh.py]
---

# Management Quote Coverage Follows Owned Positions

## What We Learned

An entry scanner's DTE window is not a valid quote universe for lifecycle
management. Management must request every unexpired expiration present in the
legs it already owns, even when that expiration is now below the minimum DTE for
a new entry.

## Context and Evidence

The unified cutover reused the planner's `option_chain_start_dte=21` market
provider. On 2026-08-17, two valid live positions had September 4 legs at 18 DTE,
so management could not mark them and failed with `active leg quote missing`.
The retired manager had explicitly merged open-position expirations into its
quote request. Commit `9ea9f1e` restored that invariant in the unified manager;
the first natural post-deploy run evaluated all five live groups successfully.

## When It Applies

Apply this to any multi-leg strategy whose position ages outside its entry
eligibility window. Expired or malformed leg dates remain excluded and fail
through normal lifecycle validation.

## Apply It Next Time

During an ownership migration, inventory the data required by existing state,
not only the data required to create new state. Test an open position just below
the entry DTE floor and prove the exact expiration reaches the broker adapter.

## Dead Ends

- Enlarging the entry window couples strategy selection to management and may
  admit trades that should remain ineligible.
- Treating a missing mark as a hold silently skips exits and is not safe.
