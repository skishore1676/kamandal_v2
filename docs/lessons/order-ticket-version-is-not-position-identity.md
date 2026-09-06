---
title: An order ticket version is not a position identity
type: decision
area: live execution and reconciliation
date: 2026-08-23
tags: [broker, fills, lineage, reconciliation, replacements, multi-broker]
refs: [src/kamandal_v2/live/order_identity.py, src/kamandal_v2/live/lineage.py, src/kamandal_v2/live/execution.py, src/kamandal_v2/live/reconciliation.py, aa54c6a]
---

# An Order Ticket Version Is Not a Position Identity

## What We Learned

Use the root entry lineage as the stable local position identity. Count a fill
once per broker order ID, using its maximum cumulative `filledQuantity`.
Keep the deterministic client order ID and broker-assigned order ID as separate
persisted fields; never rewrite ticket identity merely because a broker returns
its own numeric ID.

## Context and Evidence

An atomic reprice produced a parent and child ticket with one broker order ID.
Projecting a group per ticket doubled local exposure while the broker still held
one spread. The inverse edge also exists: a staged cancel-and-replace can produce
two broker order IDs that each partially fill, so keeping only the latest ticket
would lose real exposure.

The regression matrix covers atomic reprices, staged replacements, cumulative
polls, terminal partial fills, ambiguous siblings, missing/cyclic parents,
historical duplicate repair, and post-fill position-endpoint lag.

The multi-broker route added a second collision domain. Public can echo a
client UUID while Tastytrade assigns a numeric order ID, and the same OCC
contract can exist in both accounts. The safe keys are therefore distinct:
client order ID for correlation and lineage, broker order ID for GET/cancel/
replace, and `(execution_venue, OCC symbol)` for position reconciliation.

## When It Applies

Apply this to entry submission, repricing, fill projection, reconciliation,
position risk, and any future execution export. Do not use it to auto-adopt an
unmatched broker position: broker exposure does not prove application ownership.

## Apply It Next Time

When adding an order lifecycle feature, ask three separate questions: which
ticket version describes the intent, which broker order IDs actually executed,
and which root lineage owns the position. If those collapse to one identifier,
the design will either double count an atomic replacement or lose a staged
partial fill. Also ask which venue owns each broker ID and position; never let
the same OCC symbol at two brokers aggregate before reconciliation.


## Tastytrade uncertainty and delayed fills (September 6)

A deterministic client ID is not broker-side deduplication. Tastytrade's
`external-identifier` is correlation only. In `live/execution.py`, persist
`submit_uncertain` before POST. On a lost acknowledgement, retain the reservation,
block retry/fallback, and bind only a unique broker lookup result. Tests in
`tests/test_live_lane.py` verify both recovery without another POST and an absent
match remaining blocked.

A Filled status can arrive before per-leg fills. In `market/tastytrade.py`, wait
for complete returned-leg quantities and calculate package price from signed
actual fill cashflows. Do not adopt the limit price as an execution receipt.
`tests/test_tastytrade_adapter.py` covers entry and mixed close/open roll prices.

See the [official retry contract](https://developer.tastytrade.com/docs/guides/idempotency-and-retries/)
and [order lifecycle](https://developer.tastytrade.com/docs/concepts/order-lifecycle/).
