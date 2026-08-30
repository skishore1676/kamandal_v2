# Short Strangle Experiment: Current State and Target

Updated: 2026-08-30
Status: shadow evidence collection; not approved for live activation

## Stable picture

Kamandal selects the trade, applies portfolio and lifecycle policy, and retains the
shadow evidence. Public supplies the current option-chain quotes. Tastytrade is the
intended execution venue and currently supplies dry-run BPR/preflight evidence;
DXLink quote streaming is not integrated. Cartographer's current-range answer is
available as descriptive research context but is no longer an admission gate.

## Current verified state

- The experiment is `mode=shadow`, one contract, with all enabled universe profiles
  source-eligible. Price/IV, 35-50 DTE, 14-22 delta, earnings, quote, BPR, portfolio,
  concentration, and lifecycle gates remain active.
- Four complete historical shadow strangles exist: three winners and one loser,
  totaling -$10 gross before fees. Five entries were missed. This is machinery
  evidence, not enough economic evidence for promotion.
- The current week produced 39 candidates and no plans: 19 were rejected for low OI,
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
   source-ready and still requires session-boundary deployment/readback.
4. A natural scheduled run produces at least one reviewable shadow plan/fill or a
   truthful zero-candidate receipt whose blockers are strategy facts rather than
   machinery defects.
5. Tastytrade production preflight, account BPR/capacity, lifecycle management, and
   reconciliation are read back before the separate operator gate for pilot live.

## Promotion decision

Do not activate live yet. The machinery is substantially present, but this week's
zero-trade result was dominated by a liquidity-parity bug and the now-retired chart
veto rather than a clean strategy-selection result. Reassess after the fixes are
deployed at a session boundary and one natural shadow cycle is observed.
