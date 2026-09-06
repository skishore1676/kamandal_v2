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

The Sheet row remains `shadow`. A later live pilot is a separate protected
change. No deployment or Sheet migration in this design implies permission to
submit real orders.

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
Tastytrade dry-run BPR receipts. Before the protected live flip, refresh that
evidence, read production account capacity and reconciliation readiness, and then
perform a separately approved bounded one-contract canary with current live quote
evidence. A separate certification sandbox is optional and does not prove fill
quality or strategy economics.

See [TASTYTRADE_LIVE_HANDOFF.md](TASTYTRADE_LIVE_HANDOFF.md) for the secure
credential placement and staged broker-validation procedure.

See [September 6 readiness review](reviews/strangle-tuesday-readiness-2026-09-06.md)
for current proof, exact-source policy changes, and official API findings.

For submission recovery and fill adoption changes, read the
[order identity lesson](lessons/order-ticket-version-is-not-position-identity.md).
