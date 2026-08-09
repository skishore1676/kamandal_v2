# Strategy Promotion Loop

Status: source contract and guarded live adapter implemented; deployment is operator-gated

## Ownership

- **Kamandal owns capability:** builders, admission, tickets, lifecycle management,
  execution adapters, safety validation, and factual receipts.
- **Google Sheet owns composition and stage:** each playbook row selects parameters,
  source mode, and exactly one of `baseline`, `shadow`, `pilot_live`, or `live`.
- **TradeLab owns recommendations:** it consumes Kamandal evidence and proposes
  `continue_shadow`, `modify`, `promote_to_pilot_review`, or `demote_to_shadow`.
- **The operator owns effects:** recommendations never change a Sheet cell or authorize
  an order. Lathi may project the Board but does not own trading semantics.

## Exclusive stage routing

| Sheet `csa_stage` | Runtime owner | Effect boundary |
| --- | --- | --- |
| blank / `baseline` | established planner and live pipeline | existing approval policy |
| `shadow` | strategy capability engine + shadow adapter | CSA tables only; no broker effect |
| `pilot_live` | strategy capability engine + guarded live adapter | one staged intent per scan, one-contract cap |
| `live` | strategy capability engine + guarded live adapter | one staged intent per scan, normal Sheet/live limits |

Shadow and live scans are separate scheduled commands. A live scan never calls the
broker: it writes one stage-authorized intent to the existing live ledger. The existing
live submitter then re-reads the Sheet and requires the same playbook, stage, and policy
hash before applying health, BPR, concentration, preflight, submission-window, and
serialization gates. Demoting or changing the row therefore revokes a pending intent.

The first live-management contract is deliberately `close_only`. Every row intended
for `pilot_live` or `live` must already contain
`management_policy_json.lifecycle.live_management_mode=close_only`; otherwise policy
compilation fails closed. This keeps a stage flip honest while live CSA adjustment
actions remain a separate future capability.

## Evidence and recommendation contract

Daily scorecards use `kamandal.strategy_experiment_evidence.v1`. `NO_DATA` means the
schema or natural run evidence is absent; it is never GREEN. `COLLECTING` means the
machinery ran without errors or broker effects. TradeLab requires three natural run
days and at least one completed shadow fill before it may propose a pilot review.
That proposal is machinery readiness only and explicitly carries no alpha claim.

## Operating sequence

1. Publish and deploy tested source at a session boundary, then read back the exact
   oldmac commit and monitored job health.
2. Migrate existing playbook rows so every row has one unambiguous stage and every CSA
   row carries the close-only live-management contract. Do not reuse
   low-IV calendar rows for an earnings experiment; create separately identified rows.
3. Run three or more natural shadow sessions and let TradeLab publish its recommendation.
4. If the operator accepts a pilot recommendation, confirm there are no working shadow
   orders for that policy and change only `csa_stage` to `pilot_live`.
5. Treat a later change to `live` as a separate operator decision. TradeLab never writes
   the stage and neither recommendation state can place an order.
