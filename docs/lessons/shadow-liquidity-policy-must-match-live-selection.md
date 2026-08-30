---
title: Shadow liquidity policy must match live selection
type: bug
area: strangle-planning
date: 2026-08-30
tags: [shadow, live-parity, liquidity, open-interest, pricing]
refs: [src/kamandal_v2/planner/candidate_builder.py, src/kamandal_v2/live/pricing.py, src/kamandal_v2/strategy_engine/planning.py]
---

# Shadow Liquidity Policy Must Match Live Selection

## What We Learned

Shadow is promotion evidence for the live decision path. A liquidity rule cannot
reject a package in shadow while admitting that same package in live. For the short
strangle experiment, OI below the Sheet threshold is a warning that demands a better
credit, not a strategy rejection.

## Context and Evidence

`low_oi_mode=price_through` was guarded by `runtime.mode == live`, so 19 candidates
were rejected during a zero-plan shadow week. Live pricing already increased the
credit request from 10% toward 20-35% of the aggregate spread as OI deteriorated.
The shadow ticket still started at midpoint, so merely relaxing admission would not
have tested the intended execution behavior.

## When It Applies

Apply this to `short_strangle`/`strangle` candidates explicitly marked
`low_oi_price_through=true`. Quote validity, absurd/wide package checks, BPR, and risk
limits remain independent hard gates. Other structures retain their established
shadow pricing until their policy is reviewed separately.

## Apply It Next Time

When a shadow/live funnel diverges, compare candidate rejection reasons before
changing Sheet thresholds. Preserve the warning in the candidate, freeze the
liquidity-adjusted credit into the shadow ticket, and cap the initial ask by the
ticket's configured concession capacity so a paper order can traverse its full
natural retry ladder.

## Dead Ends

Do not lower `min_option_oi` to hide the mismatch; that discards evidence instead of
testing price-through. Do not apply the live campaign blindly to every shadow ticket:
debit tickets require signed economics, and unrelated strategies have separate fill
contracts.
