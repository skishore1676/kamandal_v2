# Short-Strangle Pilot Runbook

Updated: 2026-09-05
Pilot date: Tuesday, 2026-09-08  
Current state: `REPAIR_VERIFIED_AWAITING_TUESDAY_GATES`

## Authorized outcome

On 2026-09-05 Suman authorized a new conditional one-contract Tastytrade pilot
for Tuesday, 2026-09-08 after the two blockers behind the dated Friday `NO_GO`
were repaired. The Friday result remains historical truth; this is a new pilot
plan, not a retroactive change to that decision.

The operator row must stay `mode=shadow`, `csa_stage=shadow` through the Monday
market holiday. Before Tuesday's immutable daily policy snapshot, the current
deployed-version, Sheet-policy, account, reconciliation, overlap, and health gates
must be green. Only then may the two operator cells change to `mode=live`,
`csa_stage=pilot_live`. Fresh quote, candidate, exact-leg Tastytrade dry-run BPR,
concentration, portfolio, earnings, and execution-window gates remain owned by the
normal planner and executor. They must all pass before any broker submission.

This ordering is deliberate: Kamandal freezes Sheet policy once per trading day,
so an edit after the first planner would not take effect until the next trading
day. Sheet arming authorizes Tuesday's natural engine to consider the canary; it
does not assert that a candidate exists or bypass the dynamic execution gates. If
there is no qualifying candidate or any dynamic gate fails, no broker order is
submitted.

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
| `REPAIR_VERIFIED_AWAITING_TUESDAY_GATES` | The two Friday code blockers are repaired and the operator has authorized a new bounded attempt. The Sheet is still shadow. | Fresh Tuesday pre-snapshot static gates may set `PILOT_ARMED`; otherwise stay shadow. |
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

The finite heartbeat `arm-tuesday-strangle-pilot` has exactly two scheduled
wakeups on 2026-09-08: 07:45 CT for the conditional pre-snapshot Sheet gate and
14:45 CT for readback and return-to-shadow. It may edit only the two stage/mode
cells described here and must never trigger a trading job or broker action.

- Monday, August 31 through Thursday, September 3: inspect each natural day's
  policy snapshot, planner funnel, fill campaign, BPR source, health, and broker
  effects. Keep the row shadow.
- Wednesday/Thursday: re-read actual Tastytrade capacity after capital arrives.
  Missing capital before Thursday is not an exception requiring operator review.
- Friday, September 4 after the natural report: record one explicit `GO` or
  `NO_GO` with failed gate names and evidence paths. `NO_GO` means stay shadow.
- Monday, September 7: US market holiday; do not promote or submit.
- Tuesday, September 8 before the daily policy snapshot: independently re-run
  deployed-version, current-account, Sheet-policy, health, overlap, and
  reconciliation gates. Promote only if every static gate is green. Do not
  trigger a planner, executor, report, or broker cycle by hand.
- At 08:50 CT and later natural windows: require the normal engine to obtain fresh
  quotes, construct a qualifying candidate, obtain usable exact-leg Tastytrade
  dry-run BPR, and pass every remaining portfolio, concentration, earnings,
  broker, health, and execution-window gate before submission.
- After promotion: let the normal planner and executor own selection and
  submission. Observe the resulting lifecycle through normal reporting. The
  canary reservation prevents a second lifecycle for that pilot policy.
- If the final September 8 planner window ends without a canary reservation,
  restore both Sheet controls to shadow. This authorization does not roll into
  a later trading day merely because no candidate qualified on Tuesday.

## Exception and rollback policy

Known outcomes have known actions: a failed gate, no qualifying candidate,
insufficient capacity, a provider outage, or an unresolved health condition leaves the row in shadow and
does not create an Obsidian request. Publish a decision packet to the Northstar
coding-agent drawer through Lathi Bus only when an unresolved judgment is required
and this runbook does not say what to do.

After arming, return the row to shadow if policy identity changes unexpectedly,
the pilot cap cannot be proven, broker/account reconciliation is ambiguous, or a
submission produces an unexplained state. Do not cancel, replace, or close a real
order outside Kamandal's existing lifecycle rules unless separately authorized.
