# Short Strangle Experiment: Current State and Target

Updated: 2026-08-31 after the natural close
Status: shadow evidence collection; conditionally authorized for one pilot on 2026-09-08

## Stable picture

Kamandal selects the trade, applies portfolio and lifecycle policy, and retains the
shadow evidence. Public supplies the current option-chain quotes. Tastytrade is the
intended execution venue and currently supplies dry-run BPR/preflight evidence;
DXLink quote streaming is not integrated. Cartographer's current-range answer is
available as descriptive research context but is no longer an admission gate.

## Current verified state

- The experiment is currently `mode=shadow`, one contract, with all enabled universe profiles
  source-eligible. Price/IV, 35-50 DTE, 14-22 delta, earnings, quote, BPR, portfolio,
  concentration, and lifecycle gates remain active.
- Four complete historical shadow strangles exist: three winners and one loser,
  totaling -$10 gross before fees. Five entries were missed. This is machinery
  evidence, not enough economic evidence for promotion.
- The prior week produced 39 candidates and no plans: 19 were rejected for low OI,
  16 for unavailable/insufficient Cartographer evidence, two for wide spreads, and
  two for Tastytrade preflight errors.
- Low OI was an unintended shadow/live parity bug. The intended policy is warning plus
  a higher-credit entry campaign, not rejection.
- Sixteen candidates were blocked by missing or insufficient Cartographer evidence.
  That was a policy-design failure, not evidence that those trades were invalid:
  TLT/IEF failed a chart freshness contract and MSTR had no Mala cache partition,
  while the classifier itself described an existing range rather than forecasting
  future containment.
- Public HTTP 429 on 2026-08-27 was a quote-feed rate limit, not a Tastytrade failure.
  The planner failed closed and recovered naturally the following day.

## Evidence-week readback

### Monday, 2026-08-31

- Natural unified planning created and naturally filled two one-contract shadow
  strangles: TLT at `$0.74` credit with about `$1,409` dry-run BPR, and IEF at
  `$0.33` credit with about `$1,853` dry-run BPR. Both lifecycles remain open;
  same-day marks were not actionable, so this is machinery proof rather than an
  economic result.
- Both exact-leg preflights recorded `bpr_source=tastytrade_dry_run`. The Tastytrade
  response currently blocks live eligibility for account capacity, which is the
  expected pre-funding state and must be re-read after the planned Wednesday or
  Thursday deposit.
- IEF had minimum leg OI `3`, below the configured `50`, and was retained with
  `filter_warning=open_interest_below_min` plus `low_oi_price_through=true`. This
  is direct natural proof that low OI now changes pricing rather than vetoing the
  shadow observation.
- The frozen row remained `mode=shadow`, `csa_stage=shadow`, one contract,
  `tasty_primary`, and `range_gate_required=FALSE`. The plans contain no
  Cartographer range blocker and created no Sheet, stage, order, or broker effect.
- Runtime finished GREEN with no active run error. One SQLite lock error was
  recovered within the natural schedule. Live health was YELLOW only because META
  already holds its per-underlying position cap; there were no reconciliation
  blockers or pending entry orders.

## Target before pilot live

1. Shadow and live classify low-OI packages identically; shadow freezes the
   higher-credit price-through decision into the ticket within its configured
   concession capacity.
2. The retired Cartographer fields remain inert Sheet compatibility columns.
   A future `TUSSLE_EXPECTED` classifier, if built, is recorded beside the baseline
   as non-blocking research evidence and must prove incremental value before gaining
   selection authority.
3. Public calls are paced and HTTP 429 responses retry with provider-directed or
   exponential backoff; a quote outage remains fail-closed. This correction is
   deployed on oldmac and still needs natural-week evidence.
4. A natural scheduled run produces at least one reviewable shadow plan/fill or a
   truthful zero-candidate receipt whose blockers are strategy facts rather than
   machinery defects. Monday supplied two reviewable fills; the remaining week
   must demonstrate repeatable scheduling and lifecycle evidence.
5. Fresh natural production `tastytrade_dry_run` BPR, actual account capacity after
   the expected Wednesday/Thursday funding, lifecycle routing, and reconciliation
   readiness are read back before Tuesday activation. A separate certification
   sandbox is optional.

## Promotion decision

Keep the row shadow through the evidence week. Suman has authorized promotion on
Tuesday, 2026-09-08 only if the Friday, 2026-09-04 checklist recommendation is `GO`
and Tuesday's final account and safety readback remains green. That promotion permits
the normal planner and executor to submit one qualifying one-contract Tastytrade
canary; otherwise the row remains shadow. The authoritative checklist and state
machine are in [STRANGLE_PILOT_RUNBOOK.md](STRANGLE_PILOT_RUNBOOK.md).
