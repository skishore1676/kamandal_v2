# Multi-broker autonomous strangle lane

## Decision

Run one Kamandal brain, not a cloned application. The Google Sheet owns the
policy and future-entry venue; a persisted trade owns its immutable venue for
the rest of its lifecycle.

## End-to-end flow

1. The enabled short-strangle playbook filters IV percentile, IV rank, DTE,
   target DTE, delta, earnings, liquidity, and sizing from Sheet values.
2. The normal optimizer compares the candidate with the rest of the family
   book and applies both aggregate exposure and venue-local buying-power caps.
3. The selected lifecycle freezes the policy hash and `execution_venue`.
4. Shadow uses the existing broker-inert lifecycle simulator. A future live
   ticket uses the adapter mapped to its frozen venue for preflight, submit,
   replace, status, adjustment, and close.
5. Profit, 21-DTE, half-time, pre-event, tested-side adjustment, and 3x
   buyback-cost loss rules manage the package from the frozen entry policy.
   Duration extension and inversion are unsupported and rejected by compilation.
6. Exact Mike/Greg strangles can join this same path with unchanged contracts,
   explicit source structure scope, fresh source evidence, and all entry gates.

Market Cartographer's deterministic `range_regime` answer remains descriptive
chart context, not a strangle admission rule. A current horizontal range does not
predict that price will remain contained over the option holding period and can
precede expansion. If a future forward-looking `TUSSLE_EXPECTED` classification is
developed, Kamandal will first retain it as non-blocking experimental evidence and
compare its incremental economics with the options-only baseline.

## Promotion boundary

The September 8 operator activation promoted `short_strangle_high_iv` to normal
live execution at `tasty_primary`. The original shadow/pilot design is historical;
future policy or venue changes still require their own operator gate. A persisted
position keeps its frozen venue and management policy.

The broker contract is now explicit:

- `order_id`/`client_order_id` is Kamandal's deterministic correlation and
  lineage identity. `broker_order_id` is the id assigned by the routed broker.
  Poll, cancel, and replace always use the latter; broker assignment never
  rewrites the former. Tastytrade external identifiers do not deduplicate POSTs.
  A write-ahead `submit_uncertain` ledger state prevents blind retry; recovery
  queries the broker and binds a unique matching order before proceeding.
- Tastytrade order calls pin the Orders API version separately from unrelated
  API surfaces. Atomic replacement first calls the replacement dry-run and only
  then PATCHes price/type/time-in-force on the current broker order, without legs.
  A tested-side roll instead creates a new atomic two-leg close/open order.
- Tastytrade live account, position, preflight, submit, status, cancel, and
  replace operations fail closed unless the target account number is explicitly
  configured. Automatic "first account" discovery is not live authority.
- Position reconciliation collects every venue implicated by an open group or
  working ticket. Its comparison key is `execution_venue + OCC symbol`, so an
  identical option held at Public and Tastytrade cannot offset or hide a
  discrepancy at the other broker. If a required venue inventory is
  unavailable, repair is suspended and the venue is reported as unavailable.
- A Tastytrade Filled status alone is insufficient: all returned legs need
  complete fills. Actual fill cashflows determine package price; delayed details
  remain pending and are polled again. Limit prices are not fill evidence.

Market data and execution remain deliberately separate. Public/shared quotes
may build and manage the strategy; Tastytrade supplies native dry-runs, order
receipts, status, and positions for its venue. DXLink is not yet a Kamandal
quote provider. Natural shadow planning has already produced exact-leg production
Tastytrade dry-run BPR receipts. Native BPR and current account capacity still
gate every live opening or risk-opening adjustment. A separate certification
sandbox is optional and does not prove fill quality or strategy economics.

See [TASTYTRADE_LIVE_HANDOFF.md](TASTYTRADE_LIVE_HANDOFF.md) for the secure
credential placement and staged broker-validation procedure.

See [September 6 readiness review](reviews/strangle-tuesday-readiness-2026-09-06.md)
for current proof, exact-source policy changes, and official API findings.

## Frozen management rules and October 12 readback

The owner readback at 03:03 UTC October 13 (22:03 CT October 12) retained two
open live Tastytrade strangles, DHR and NTAP. Both use frozen policy hash
`a18a806f1ae492cfdd4258421375011e9a1e37dc83c23ebd93d27af4f7e91de3`.
Effective `mode=live`, `csa_stage=live`, and `execution_venue=tasty_primary` control
the route; old descriptive text saying “shadow only” or referring to Public
entitlement does not override those fields. This documentation does not rewrite
the Sheet or either position's frozen hash.

| Rule | Runtime behavior for these frozen positions |
| --- | --- |
| Entry | One contract; broker-native BPR capped at $2,500 per order plus shared portfolio and venue capacity. |
| Profit | Frozen target is 40% of original opening credit. With platform resting-profit enabled, offer the exact target as a DAY order at the first valid maintenance observation, including negative current P&L. Filled adjustment credits affect the buyback amount, without resetting target dollars. |
| Time | Close at 21 DTE or at half of the original holding duration, whichever becomes due first. Original duration uses the first actual fill date; the threshold is whole days rounded down. |
| Event | Mandatory close within five calendar days before captured earnings, under the frozen earnings-avoidance policy. An uncertain event does not veto independently valid time risk. |
| Loss | A package buyback cost at least three times original opening credit proposes the loss close, then shared adverse-loss confirmation and opening/closing buffers apply. This is a buyback-cost ratio: without adjustments, 3x cost means a net loss of 2x opening credit. |
| Tested side | Two distinct observations of a tested short strike, after a 30-minute cooldown, permit a paired close/open roll of the **untested** side in the same expiry. Target replacement delta is 0.30, bounded by 0.40; natural credit must be at least $0.10. |
| Adjustment bounds | At most two **filled** adjustments. Two inside-strike observations rearm a consumed episode. A proposal or unfilled attempt does not consume a filled adjustment. No duration extension or inversion. |

Working/ownership conflicts outrank action proposals. The arbiter orders emergency,
mandatory event, executable profit, time, adverse loss, adjustment, resting target,
then hold. Thus a time-close duty cannot be postponed by a tested-side roll or a
patient profit offer. Emergency is effective when supplied by runtime context;
the ordinary context does not independently manufacture an emergency trigger.

The retained nested `watch_multiple=2` is not an independent watch action in
the strangle proposer; the implemented threshold is the 3x close followed by
shared debounce. Nested `fill.max_attempts=4` describes shadow fill policy, not
four live exit concessions. Live forced closes follow the shared midpoint →
halfway → recorded natural boundary ladder, ten minutes apart with two
replacements maximum. Target offers never concede. Valid wide/zero-bid closing
quotes can qualify, while stale, crossed, incomplete, or nonfinite quotes cannot.
Roll-opening legs retain entry checks. See [EXIT_EXECUTION.md](EXIT_EXECUTION.md).

DHR holds the October 30 $190 put/$235 call. Its actual retained opening credit
is $543, filled September 16. At the October 12 observation it had 18 DTE, below
both 21 DTE and its 22-day half-time threshold, so **time exit wins**. Its $5.50
midpoint close and $7.30 natural boundary are historical pricing evidence. The
ticket was deferred after the submission window; the position remains open and
there is no close fill. The recovery repair lets the next natural maintenance
observation refresh the same-price deferred duty, while retaining all current
quote, ownership, session, and broker checks.

NTAP holds the November 20 $200 put/$270 call, with actual opening credit $970
filled October 2. Its frozen target is $388. With no filled adjustment, the
target buyback is $5.82, rounded to the allowed $5.80 limit. At the October 12
observation, 39 DTE was above its time boundaries; a first-valid-observation
resting target is therefore eligible unless a higher-priority duty intervenes.
An offered target is not a fill, and its economics remain unproven until broker
confirmation.

The October 12 recovery tests exercise Tastytrade native payload construction,
after-hours deferral, next-session same-price recovery, two bounded replacements,
and one canonical fill in an isolated database with a fake broker. Native adapter,
adjustment-episode, and reconciliation tests cover the separate paths. These are
broker-inert acceptance tests; the next natural oldmac session must still supply
the actual recovery, order, replacement, and fill receipts.

For submission recovery and fill adoption changes, read the
[order identity lesson](lessons/order-ticket-version-is-not-position-identity.md).
