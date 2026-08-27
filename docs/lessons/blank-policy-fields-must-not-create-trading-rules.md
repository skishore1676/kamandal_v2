---
title: Blank policy fields must not create trading rules
type: bug
area: directional diagonal construction
date: 2026-08-27
tags: [google-sheet, diagonals, policy, candidate-builder]
refs: [src/kamandal_v2/planner/candidate_builder.py, src/kamandal_v2/strategy_lanes/builders.py, tests/test_directional_diagonal_selection.py]
---

# Blank Policy Fields Must Not Create Trading Rules

## What We Learned

A blank operator field means the operator did not impose that constraint. It
must not activate a repository default that materially changes a trade.

## Context and Evidence

Directional-diagonal `spread_width` was blank in Google Sheets. The direct
builder interpreted blank as `$5`, while the unified adapter interpreted it as
one option-chain strike interval. Both paths therefore let code select the long
strike relative to the short strike instead of independently honoring the
Sheet's long-leg delta and DTE policy.

The corrected selector independently targets the midpoint of each leg's
Sheet-owned DTE/delta ranges. Call and put behavior share the same mirrored
implementation. Strike distance is recorded after selection and never enters
the selection score.

## When It Applies

Use this rule whenever a Sheet value is optional and a fallback could change
eligibility, strikes, expirations, quantity, money, or lifecycle behavior.
Presentation defaults are harmless; trading-policy defaults are not.

## Apply It Next Time

For every optional policy field, test three cases: explicit value, blank value,
and missing value. The blank/missing tests must prove either a documented
capability invariant or a fail-closed result. They may not reveal a hidden
trading parameter.

## Dead Ends

Deriving a width from the strike grid looked more market-aware than `$5`, but it
still invented an operator rule and therefore preserved the architectural bug.
