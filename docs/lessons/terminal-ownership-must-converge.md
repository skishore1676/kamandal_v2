---
title: Terminal ownership must converge across lifecycle, order, and projection state
type: pattern
area: lifecycle management and reconciliation
date: 2026-08-21
tags: [lifecycle, reconciliation, projections, terminal-state, economics]
refs: [c2a9f6c, src/kamandal_v2/strategy_engine/ownership.py, src/kamandal_v2/live/reconciliation.py, src/kamandal_v2/strategy_engine/history.py]
---

# Terminal Ownership Must Converge

## What We Learned

A position or entry is not terminal until every ownership surface agrees. A
closed broker position with an open lifecycle can trade again; an exhausted
entry lineage with a pending lifecycle remains a false owner.

## Context and Evidence

After the unified cutover, GLD's live projection was correctly
`reconciled_retired` when the broker was flat, but its canonical lifecycle
remained open and continued through five-minute management. A separate ADBE
entry exhausted three replacement tickets while its lifecycle remained
`pending_live_submission`.

Commit `c2a9f6c` makes reconciliation converge those states. It closes a
lifecycle for a retired projection only after affected broker quantities equal
the remaining open local ledger. It marks a pending entry `entry_missed` only
after its complete guarded ticket lineage is terminal.

## When It Applies

Apply this rule to complete fills, manual/external closes, broker-flat
reconciliation, abandoned entries, and replacement lineages. A Sheet row or
read model is never sufficient proof by itself.

## Apply It Next Time

When a manager's lifecycle count exceeds the live-book group count, compare the
canonical lifecycle, complete order lineage, projection status, and broker
aggregate before changing strategy rules. Terminalize through reconciliation;
do not revive an old manager or invent a missing close fill.

If close economics were not captured, preserve the terminal leg snapshot and
label economics `reconciled_without_fill`. Never manufacture realized P&L to
make reports look complete.
