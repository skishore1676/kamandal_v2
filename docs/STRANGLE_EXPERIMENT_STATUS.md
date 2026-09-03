# Short Strangle Experiment: Current State and Target

Updated: 2026-09-02 after the natural close
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

### Tuesday, 2026-09-01

- All four natural unified-planning windows completed without policy or runtime
  errors. No additional shadow strangle was opened because the TLT and IEF
  lifecycles already own the eligible observations; both remained open and were
  refreshed through the natural lifecycle-management schedule.
- The experiment scorecard advanced to `COLLECTING`, with no unexpected broker,
  order, Sheet, or stage effect. The open-lifecycle marks still lack an actionable
  price/P&L value, so the day adds repeatability evidence but not economic evidence.
- The morning correspondent-source schema mismatch failed closed twice and then
  recovered on its scheduled 11:45 run. It did not interrupt the market-scan
  strangle lane.
- Current live health is RED for two cancelled close orders in Kamandal's existing
  live portfolio. Reconciliation itself is GREEN and the strangle row remains
  shadow, but unresolved failed closes are a pilot-readiness blocker: Friday cannot
  be `GO` unless the normal live-order recovery or operator handling clears them
  and a later health readback is green.

### Wednesday, 2026-09-02

- The frozen policy snapshot remained unchanged and valid at
  `40f9f66b35418016740ec3693c6ff01c8b469dc8a1bbd3b11c5fcb5bd7b015de`:
  `mode=shadow`, `csa_stage=shadow`, one contract, `tasty_primary`, full enabled
  universe expansion, `$2,500` maximum per-order BPR, and
  `range_gate_required=FALSE`.
- The natural shadow planner produced a truthful strategy-driven zero-candidate
  receipt: 94 in-universe ideas were evaluated, none matched every current IV,
  price, event, and universe gate, and no candidate or plan was constructed.
  The latest receipt contains no Cartographer range rejection, low-OI veto,
  preflight failure, or active planning error.
- Both Monday strangles remain open. Same-day validated midpoint marks now show
  `$7.50` combined unrealized P&L on `$3,271.14` recorded open BPR. This proves
  that the natural mark/economics path is operating; it is not a realized result
  or evidence of strategy expectancy.
- Deployed commit `d972060` makes shadow account and BPR capacity observational
  rather than an admission veto and records provider-aligned daily volatility
  evidence. Tastytrade supplies daily absolute IV and percentile when available;
  IV Rank remains an explicitly labeled local-history fallback when the broker
  response does not provide it. No new qualifying package existed today, so the
  Monday exact-leg Tastytrade dry-runs remain the latest natural BPR evidence.
- The two cancelled close orders are now correctly classified as routine unfilled
  resting profit orders rather than failed closes. Live health improved from RED
  to YELLOW with zero failed closes and zero reconciliation blockers; the remaining
  warnings are self-handled META and megacap-tech position caps. The prior failed-
  close pilot blocker is therefore cleared.
- A read-only Tastytrade account check still showed only `$200` of account value
  and buying power. Sufficient post-funding capacity is not yet proven and remains
  a Friday/Tuesday activation gate. One transient SQLite lock recurred and was
  recovered by the bounded retry path; no Public HTTP 429 appeared in the day's
  retained events.

### Thursday, 2026-09-03

- The natural policy snapshot moved to
  `5e768ab4bddf065e7b4344abf3e7dc8c73d540096990bcef4874bdce2b8195e6`
  after the intentional IV-policy alignment in `d972060`. The row remains
  `mode=shadow`, `csa_stage=shadow`, one contract, `tasty_primary`, full enabled
  universe expansion, `$2,500` maximum per-order BPR, and
  `range_gate_required=FALSE`. IV Rank `50-100` is now the sole volatility
  admission gate; the IV-percentile cells are intentionally blank and percentile
  remains evidence rather than a co-gate.
- The natural shadow planner evaluated 100 in-universe ideas and truthfully built
  zero candidates because no market-scan symbol cleared every current IV Rank,
  price, event, and universe condition. There was no Cartographer range rejection,
  low-OI veto, preflight failure, active run error, Public HTTP 429, or unexpected
  broker effect.
- Both shadow strangles remain open on `$3,271.14` recorded BPR. Only one had an
  actionable same-day package mark, so aggregate unrealized P&L correctly returned
  to unavailable rather than carrying Wednesday's `$7.50` value forward.
- The Tastytrade deposit is visible as `$10,200` buying power with zero positions.
  This clears the simple cash-capacity concern for a candidate within the `$2,500`
  order cap, but a new natural candidate did not exist, so Monday remains the latest
  exact-leg production dry-run BPR receipt.
- Reconciliation completed with zero blockers and two existing live positions
  closed naturally. However, the latest Public-backed live account snapshot reports
  `$9,040.91` BPR used on `$11,951.93` account value (`75.64%`), above the `55%`
  hard portfolio cap. Live health is RED and correctly blocks new entries. The
  five open groups account for only `$2,317` in the per-underlying BPR breakdown,
  so the discrepancy requires either a later normalized broker snapshot or an
  explained account-level obligation before Friday can be `GO`. Two close actions
  deferred after the product cutoff have a defined next-session self-healing path.

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
