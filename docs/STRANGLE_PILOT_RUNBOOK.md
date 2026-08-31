# Short-Strangle Pilot Runbook

Updated: 2026-08-30  
Pilot date: Tuesday, 2026-09-08  
Current state: `SHADOW_COLLECTING`

## Authorized outcome

Suman has authorized a conditional one-contract Tastytrade pilot. If the
shadow-week checklist passes and the Friday, 2026-09-04 recommendation is `GO`,
the operator row may be changed on Tuesday, 2026-09-08 from `mode=shadow`,
`csa_stage=shadow` to `mode=live`, `csa_stage=pilot_live`. The normal planner and
executor may then submit one qualifying short-strangle lifecycle. If either the
Friday decision or Tuesday final checks fail, the row stays in shadow and no
broker order is submitted.

This authorization does not permit discretionary orders, more than one canary
lifecycle, a quantity above one contract per leg, another strategy promotion,
or bypassing a planner, account, BPR, concentration, reconciliation, quote,
earnings, or execution-window gate.

## State machine

| State | Meaning | Permitted transition |
| --- | --- | --- |
| `SHADOW_COLLECTING` | Row remains shadow while natural jobs produce evidence. | Friday checklist may set `FRIDAY_GO` or `FRIDAY_NO_GO`. |
| `FRIDAY_GO` | Machinery passed; this is permission to perform Tuesday final checks, not permission to submit early. | Tuesday final checks may set `PILOT_ARMED`; any failed check returns to shadow. |
| `FRIDAY_NO_GO` | At least one required gate failed. | Stay shadow. A later pilot needs a new explicit plan. |
| `PILOT_ARMED` | On September 8 only, the row is `mode=live`, `csa_stage=pilot_live`. | The normal planner may reserve one qualifying lifecycle. |
| `CANARY_RESERVED` | The unified engine has created the one pilot lifecycle. | Normal executor may work that lifecycle; no second lifecycle is allowed for the same pilot policy. |
| `PILOT_OBSERVING` | Order is working, filled, managed, closed, rejected, or expired. | Retain receipts and recommend continue, modify, or return to shadow. No automatic expansion. |

## Shadow-week checklist

A Friday `GO` requires all of the following from natural scheduled runs:

- the current oldmac checkout and launchd jobs are healthy, with no unexplained
  planning or execution failure;
- the Sheet row is still shadow, one contract, `tasty_primary`, and eligible for
  the full enabled universe;
- the retired Cartographer range gate remains non-blocking;
- low open interest is treated as a pricing-quality warning with the configured
  higher-credit campaign, not an admission veto;
- at least one natural run yields either a reviewable plan/fill or a truthful
  strategy-driven zero-candidate receipt;
- current quotes are fresh enough for planning, and Public rate-limit recovery
  works without silently admitting stale data;
- a fresh exact-leg production Tastytrade dry-run returns usable BPR;
- production account capacity, after Suman's expected Wednesday/Thursday capital
  addition, is read from the broker and is sufficient for the candidate; no
  funding amount is assumed;
- the candidate stays within the $2,500 per-order BPR cap, one-contract sizing,
  concentration limits, and earnings policy;
- no overlapping live position, working order, unresolved partial fill, or
  reconciliation blocker exists;
- the pilot-live canary reservation tests and the relevant full test suite pass.

The checklist evaluates machinery and safe executability. It does not claim that
one shadow week proves positive strategy expectancy.

## Calendar and wakeups

- Monday, August 31 through Thursday, September 3: inspect each natural day's
  policy snapshot, planner funnel, fill campaign, BPR source, health, and broker
  effects. Keep the row shadow.
- Wednesday/Thursday: re-read actual Tastytrade capacity after capital arrives.
  Missing capital before Thursday is not an exception requiring operator review.
- Friday, September 4 after the natural report: record one explicit `GO` or
  `NO_GO` with failed gate names and evidence paths. `NO_GO` means stay shadow.
- Monday, September 7: US market holiday; do not promote or submit.
- Tuesday, September 8 before the first planner window: independently re-run all
  current-account, policy, health, quote, BPR, overlap, and reconciliation gates.
  Promote only if Friday was `GO` and every Tuesday gate is still green.
- After promotion: let the normal planner and executor own selection and
  submission. Observe the resulting lifecycle through normal reporting. The
  canary reservation prevents a second lifecycle for that pilot policy.
- If the final September 8 planner window ends without a canary reservation,
  restore both Sheet controls to shadow. This authorization does not roll into
  a later trading day merely because no candidate qualified on Tuesday.

## Exception and rollback policy

Known outcomes have known actions: a failed gate, no qualifying candidate,
insufficient capacity, a provider outage, or `NO_GO` leaves the row in shadow and
does not create an Obsidian request. Publish a decision packet to the Northstar
coding-agent drawer through Lathi Bus only when an unresolved judgment is required
and this runbook does not say what to do.

After arming, return the row to shadow if policy identity changes unexpectedly,
the pilot cap cannot be proven, broker/account reconciliation is ambiguous, or a
submission produces an unexplained state. Do not cancel, replace, or close a real
order outside Kamandal's existing lifecycle rules unless separately authorized.
