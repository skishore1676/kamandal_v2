# Live Reconciliation Contract

Kamandal treats the broker account as the source of truth for actual exposure
and its SQLite ledger as the source of truth for intent, ownership, and audit.
Reconciliation joins those truths; it does not ask the operator to perform a
comparison the application can prove itself.

## Identity model

Three identifiers describe different things:

- A **ticket hash** identifies one immutable local order version.
- A **broker order ID** identifies one broker execution stream. An atomic
  reprice can keep this ID; a cancel-and-replace creates a new one.
- An **entry lineage** begins at the original ticket and owns exactly one local
  position group, `live_group_<root_ticket_hash>`.

The position group is therefore stable across price changes. The deepest
unambiguous viable ticket supplies current metadata, while fills are aggregated
once per distinct economic broker order. Broker `filledQuantity` is cumulative,
so Kamandal keeps the maximum observed quantity for each order rather than
summing polls. When a broker-atomic replacement has different immutable local
client IDs, the parent-child replacement proof still makes those versions one
economic order.

## Deterministic recovery matrix

| Condition | Automatic behavior | Why it is safe |
| --- | --- | --- |
| Atomic reprice, same broker order ID | Keep one lineage group and one maximum cumulative fill | The broker identifies one execution stream |
| Staged replacement, different broker order IDs | Sum the maximum fill from each order into the lineage group | Distinct order IDs can each own a real execution |
| Partial fill followed by cancel/reject/expiry | Preserve the partial exposure as an open local position | A terminal order is not the same as zero exposure |
| Repeated FILLED/PARTIALLY_FILLED polls | Upsert the stable group without changing `opened_at` | Broker quantities are cumulative and projection is idempotent |
| Historical duplicate groups from one lineage | Retire duplicates only when lineage, economic legs, and complete broker quantity arithmetic all agree | The repair has three independent proofs |
| One group overcounted by atomic replacement aliases | Rebuild the projection from the maximum cumulative alias fill only when lineage, exact OCC legs, and broker quantities all agree | Ticket versions are proven aliases, not separate executions |
| Fresh fill absent from the first position snapshot | Hold during the post-fill grace window | Order and position endpoints can settle at different times |
| Confirmed broker-flat local ghost | Use the configured repeated-observation retirement path | Broker absence is confirmed, not inferred from one poll |
| Resolved issue or repaired projection | Resolve the issue, expire its review request, and replace the reconciliation Sheet lane even when empty | Stale human work must disappear when source truth clears |

## Fail-closed cases

Kamandal does not mutate ownership when replacement siblings are ambiguous,
lineage parents are missing or cyclic, economic legs differ, broker arithmetic
does not prove a repair, or the broker payload cannot be normalized. It also
does not auto-adopt an unmatched broker position because broker data alone
cannot prove which application or strategy owns externally created exposure.

Those cases create one bounded operator review only after automatic recovery is
unavailable. A broker/API failure preserves the last known state and fails the
run; it never turns an unavailable snapshot into a flat account.

## Operational outcome

Routine reprices, delayed position visibility, partial fills, and known
historical duplicate projections are app-owned recovery. The operator is asked
only for facts Kamandal cannot establish from its intent ledger plus broker
evidence, primarily unattributed external activity or genuine structural
ambiguity.
