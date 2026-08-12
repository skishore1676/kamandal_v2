# Strategy Promotion Loop

Status: source contract implemented; oldmac deployment tracked by release readback

## Ownership

- **Kamandal owns capability:** builders, admission, tickets, lifecycle management,
  execution adapters, safety validation, and factual receipts.
- **Google Sheet owns composition and stage:** each playbook row selects parameters,
  source mode, and exactly one of `baseline`, `shadow`, `pilot_live`, or `live`.
- **TradeLab owns recommendations:** its weekly Obsidian brief consumes Kamandal
  scorecards and proposes continue, modify, demote, or promotion review.
- **The operator owns effects:** recommendations never change a Sheet cell or authorize
  an order. Lathi may project the Board but does not own trading semantics.

## Exclusive stage routing

| Sheet `csa_stage` | Runtime owner | Effect boundary |
| --- | --- | --- |
| blank / `baseline` | established planner and live pipeline | existing approval policy |
| `shadow` | strategy capability engine + shadow adapter | CSA tables only; no broker effect |
| `pilot_live` | strategy capability engine + guarded live adapter | at most one new lifecycle per playbook per trading day; one contract |
| `live` | strategy capability engine + guarded live adapter | normal Sheet sizing and existing live risk limits |

At 08:15 CT, before the market opens, Kamandal reads `universe` and `playbooks` once
and writes an immutable, dated strategy-policy snapshot. Daily ideas remain a separate
input and may continue arriving afterward from My Ideas, Birdclaw/X, and configured
correspondent profiles before or between scans. Shadow entry, pilot/live entry, shadow
management, live management, and final staged-intent authorization all use the same
policy snapshot for the trading day. A Sheet edit therefore takes effect on the next
trading day's snapshot; it does not rewrite the state beneath work already staged.

The baseline shadow planner and CSA experiments share the configured shadow account.
Open baseline shadow fills and open/working CSA lifecycles reserve buying power against
that paper account. Live account positions may generate a portfolio-hedge opportunity,
but live buying power, live contract ownership, and live working orders do not veto a
broker-inert shadow observation. Pilot/live stages retain the real account and broker
safety gates.

For a Public short strangle, Public error 159 is evidence that the account lacks the
Level 4 designation required for its uncovered short legs, not an order-shape retry.
Pilot/live therefore fail closed. A shadow scan may ask Tastytrade's order dry-run for
a BPR estimate and, if needed, use the conservative local fallback; both are recorded
as shadow evidence and neither makes the playbook executable at Public.

At 15:25 CT, the CSA scorecard job writes two app-owned evidence products:

- a daily machinery scorecard covering scans, admissions, fills, management actions,
  blockers, policy identity, and broker-effect invariants; and
- a week-to-date economics packet grouped by playbook and stage, derived from the
  lifecycle cashflow ledger, terminal outcomes, BPR, and same-day natural-close marks.

The economics packet reports realized P&L, complete marked open P&L when available,
return on closed BPR, wins/losses, adjustments, and evidence-quality limitations. It
is content-digested and explicitly has no recommendation, Sheet-write, execution, or
alpha-claim authority. Shadow results exclude commissions and use the quote-based fill
model, so TradeLab must preserve those limitations in any recommendation.

TradeLab validates both products and asks its bounded Codex analyst for a stage-aware
`continue`, `modify`, `demote`, or promotion-review proposal. Codex cannot recommend
promotion unless the machinery proposal already permits the same review and the
economic packet contains at least one closed lifecycle plus complete positive total
P&L. The proposal appears in the weekly operator brief; only the operator may change
the Google Sheet.

For cross-application consumption, Kamandal exposes the same facts through the
read-only command `kamandal experiment-status --format json`. The command adapts the
existing `kamandal.strategy_experiment_evidence.v1` and
`kamandal.strategy_weekly_economics.v1` products into
`tradelab.app_experiment_status.v1`; it does not create a second experiment catalog,
compiler, database, scheduler, or authoring surface.

The live scan itself never calls the broker. It writes stage-authorized tickets to the
existing live ledger. The existing guarded submitter still owns current health, BPR,
concentration, broker preflight, submission windows, serialized submission, and order
reconciliation. A complete broker fill advances the same app-owned lifecycle used in
shadow. Live lifecycle management can close verticals/calendars, close or roll
strangles, and close or roll the short leg of diagonals through the same typed ticket
contract; there is no blanket `close_only` promotion requirement.

## Evidence and recommendation contract

Daily scorecards use `kamandal.strategy_experiment_evidence.v1`. `NO_DATA` means the
schema or natural run evidence is absent; it is never GREEN. `COLLECTING` means the
machinery ran without errors or broker effects. TradeLab requires three natural run
days and at least one completed shadow fill before it may propose a pilot review.
That proposal is machinery readiness only and explicitly carries no alpha claim.

## Operating sequence

1. Publish and deploy tested source at a session boundary, then read back the exact
   oldmac commit and monitored job health.
2. Keep every playbook row at one unambiguous stage. Reuse a row when the idea is only
   a parameter/policy variant; add code only for a genuinely new structure or lifecycle
   capability.
3. Run three or more natural shadow sessions and let TradeLab publish its recommendation.
4. If the operator accepts a pilot recommendation, change only `csa_stage` to
   `pilot_live`. Kamandal consumes it in the next trading-day snapshot.
5. Treat a later change to `live` as a separate operator decision. TradeLab never writes
   the stage and neither recommendation state can place an order.
