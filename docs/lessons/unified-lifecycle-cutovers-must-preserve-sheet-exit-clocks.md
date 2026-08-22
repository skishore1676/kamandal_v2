---
title: Unified lifecycle cutovers must preserve every Sheet-owned exit clock
type: migration
area: unified lifecycle management
date: 2026-08-22
tags: [lifecycle, google-sheet, exits, half-time, earnings]
refs: [src/kamandal_v2/strategy_lanes/management_runtime.py, src/kamandal_v2/strategy_lanes/call_vertical.py, src/kamandal_v2/strategy_lanes/diagonal.py, src/kamandal_v2/strategy_lanes/strangle.py, tests/test_unified_management.py]
---

# Unified Lifecycle Cutovers Must Preserve Every Sheet-Owned Exit Clock

## What We Learned

Moving positions to one typed lifecycle owner is incomplete unless every
operator-visible management field is translated into shared context and an
executable action. Persisting the field in a frozen policy proves authority,
not behavior.

## Context and Evidence

The unified manager preserved profit, loss, and DTE exits but initialized
`event_exit_due` to false for ordinary lanes and never computed
`half_time_exit`. The Google Sheet still carried `exit_pre_event_days` and
`half_time_exit`, so the operator surface appeared authoritative while those
two decisions could never fire.

The repair computes both clocks once in shared management context. Original DTE
starts at the completed opening fill and uses the earliest active expiration;
pre-event distance uses the latest captured earnings date and the frozen Sheet
threshold. Call verticals, generic close-only strategies, directional
diagonals, and short strangles all close their complete active package. The
specialised earnings calendar keeps its distinct post-event contract.

## When It Applies

Use this audit whenever lifecycle ownership, policy compilation, or management
dispatch changes. It applies equally to live and shadow because they share the
frozen policy and decision engine even though their final effect adapters differ.

## Apply It Next Time

Inventory every operator field that can change entry, adjustment, or exit.
For each field, prove all four links:

1. the Sheet value enters the daily/frozen policy;
2. the manager derives current context from owned-position facts;
3. the lane emits the expected full-package action and precedence; and
4. the action reaches the correct live or shadow adapter.

Use a current-position readback before deployment. Restoring a missing exit
clock may correctly make an existing position immediately due on the next
session.

## Dead Ends

- Treating a policy hash or schema column as proof that management consumes it.
- Adding the missing rule only to one lane instead of the shared context owner.
- Forcing shadow fills to create more lifecycle examples when deterministic
  replays can cover rare branches without changing natural execution evidence.
