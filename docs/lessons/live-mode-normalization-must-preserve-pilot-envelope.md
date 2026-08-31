---
title: Live mode normalization must preserve the pilot safety envelope
type: gotcha
area: unified strategy engine
date: 2026-08-30
tags: [pilot_live, canary, policy, safety]
refs: [commit 0a874c8, src/kamandal_v2/strategy_engine/policy.py, src/kamandal_v2/strategy_engine/planning.py, tests/test_unified_planning.py]
---

# Live Mode Normalization Must Preserve the Pilot Safety Envelope

## What We Learned

Normalizing Sheet routing to `mode=shadow|live` must not erase a narrower live
safety envelope. `pilot_live` is not a third execution engine, but it still owns
the one-contract, one-lifecycle canary limit at the money boundary.

## Context and Evidence

The unified compiler intentionally mapped legacy `csa_stage=pilot_live` to
`ExecutionMode.LIVE`. The live lifecycle binder then compiled every selected
policy as `CsaStage.LIVE`, so the checked-in promotion document promised a pilot
limit that the unified path did not retain. Commit `0a874c8` preserves the stage
on the ticket and lifecycle, records a pilot-policy identity, and rejects another
candidate after that policy version reserves its canary.

The reservation is intentionally per policy version rather than per trading day.
A bounded canary must not quietly become one new trade every morning.

## When It Applies

Apply this whenever several operator states share one runtime implementation:
pilot versus scaled live, read-only versus write-enabled, or any other sub-mode
whose distinction controls effects rather than algorithm selection.

## Apply It Next Time

When simplifying an enum or routing table, search for every downstream safety
meaning of the removed value. Keep the shared engine, but freeze the narrower
authority into persisted policy, tickets, and lifecycle metadata. Add a test that
creates the first reservation and proves a later candidate is rejected.

## Dead Ends

Relying on `max_contracts_per_order=1` is insufficient: it limits the size of one
order but does not prevent a second one-contract lifecycle on a later scan or day.
