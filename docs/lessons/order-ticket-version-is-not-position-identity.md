---
title: An order ticket version is not a position identity
type: decision
area: live execution and reconciliation
date: 2026-08-01
tags: [broker, fills, lineage, reconciliation, replacements]
refs: [src/kamandal_v2/live/lineage.py, src/kamandal_v2/live/execution.py, src/kamandal_v2/live/reconciliation.py, docs/LIVE_RECONCILIATION.md]
---

# An Order Ticket Version Is Not a Position Identity

## What We Learned

Use the root entry lineage as the stable local position identity. Count a fill
once per broker order ID, using its maximum cumulative `filledQuantity`.

## Context and Evidence

An atomic reprice produced a parent and child ticket with one broker order ID.
Projecting a group per ticket doubled local exposure while the broker still held
one spread. The inverse edge also exists: a staged cancel-and-replace can produce
two broker order IDs that each partially fill, so keeping only the latest ticket
would lose real exposure.

The regression matrix covers atomic reprices, staged replacements, cumulative
polls, terminal partial fills, ambiguous siblings, missing/cyclic parents,
historical duplicate repair, and post-fill position-endpoint lag.

## When It Applies

Apply this to entry submission, repricing, fill projection, reconciliation,
position risk, and any future execution export. Do not use it to auto-adopt an
unmatched broker position: broker exposure does not prove application ownership.

## Apply It Next Time

When adding an order lifecycle feature, ask three separate questions: which
ticket version describes the intent, which broker order IDs actually executed,
and which root lineage owns the position. If those collapse to one identifier,
the design will either double count an atomic replacement or lose a staged
partial fill.
