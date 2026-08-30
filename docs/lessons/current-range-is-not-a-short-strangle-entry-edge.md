---
title: Current range is not a short-strangle entry edge
type: decision
area: strangle-planning
date: 2026-08-30
tags: [strangle, cartographer, range, implied-volatility, shadow]
refs: [commit 920a41d, src/kamandal_v2/strategy_engine/planning.py, docs/STRANGLE_EXPERIMENT_STATUS.md]
---

# Current Range Is Not a Short-Strangle Entry Edge

## What We Learned

Kamandal no longer requires Market Cartographer to return `confirmed_range` before
a short-strangle candidate can reach optimization. The Google Sheet retains the two
old range-gate columns only to preserve schema positions; they are inert.

## Context and Evidence

The retired question was post-facto: it detected whether recent daily prices formed
a horizontal range. It did not forecast whether realized movement during a 35-50 DTE
option trade would remain below the movement priced by the options. A mature range
may also precede directional expansion. Missing Cartographer data therefore blocked
shadow evidence without demonstrating that the option package was unattractive.

The strategy's primary thesis is options-economic: receive sufficient premium for
the implied movement and retain DTE/delta, event, quote-integrity, liquidity, BPR,
portfolio, sizing, and lifecycle safety gates. This matches the option-industry
description of a short strangle and Tastytrade's emphasis on implied versus realized
volatility and repeated occurrences:

- https://www.optionseducation.org/strategies/all-strategies/short-strangle
- https://www.tastylive.com/shows/market-measures/episodes/volatility-implied-vs-realized-11-09-2018
- https://www.tastylive.com/shows/market-measures/episodes/only-trade-in-high-iv-10-22-2018

## When It Applies

Do not rename the current classifier. If Cartographer later adds
`TUSSLE_EXPECTED`, define it as a forward-looking weekly/daily expectation over an
explicit horizon. Kamandal should retain that label beside otherwise eligible
shadow candidates without allowing it to veto them. Promote it to a preference or
gate only if a controlled comparison shows better realized-versus-implied movement,
strike-test frequency, adverse excursion, or net economics without starving useful
occurrences.

## Apply It Next Time

When a chart-based selector blocks an options experiment, first identify whether it
describes past structure or predicts the strategy's forward economic target. Compare
the overlay with an options-only baseline before giving it admission authority.

## Dead Ends

Do not soften freshness thresholds or rename `confirmed_range`; neither turns the
descriptive classifier into a forward forecast. Retiring this evidence gate does not
relax quote integrity, spread safety, event avoidance, buying power, concentration,
position sizing, approval, execution, lifecycle management, reconciliation, or the
separate live activation gate.
