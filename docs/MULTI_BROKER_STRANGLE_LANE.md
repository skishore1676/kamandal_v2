# Multi-broker autonomous strangle lane

## Decision

Run one Kamandal brain, not a cloned application. The Google Sheet owns the
policy and future-entry venue; a persisted trade owns its immutable venue for
the rest of its lifecycle.

## End-to-end flow

1. The enabled short-strangle playbook filters IV percentile, IV rank, DTE,
   target DTE, delta, earnings, liquidity, and sizing from Sheet values.
2. Kamandal asks Market Cartographer whether the underlying is in a fresh,
   deterministic range regime. Only `confirmed_range` is admissible.
3. The normal optimizer compares the candidate with the rest of the family
   book and applies both aggregate exposure and venue-local buying-power caps.
4. The selected lifecycle freezes the policy hash and `execution_venue`.
5. Shadow uses the existing broker-inert lifecycle simulator. A future live
   ticket uses the adapter mapped to its frozen venue for preflight, submit,
   replace, status, adjustment, and close.
6. Profit, 21-DTE, half-time, pre-event, challenged-side, duration-roll, and 3x
   loss rules manage the whole strategy package from the frozen entry policy.

## Promotion boundary

The Sheet row remains `shadow`. A later live pilot is a separate protected
change. No deployment or Sheet migration in this design implies permission to
submit real orders.

The broker contract is now explicit:

- `order_id`/`client_order_id` is Kamandal's deterministic idempotency and
  lineage identity. `broker_order_id` is the id assigned by the routed broker.
  Poll, cancel, and replace always use the latter; broker assignment never
  rewrites the former.
- Tastytrade order calls pin the Orders API version separately from unrelated
  API surfaces. Atomic replacement first calls the replacement dry-run and only
  then PATCHes the current broker order.
- Tastytrade live account, position, preflight, submit, status, cancel, and
  replace operations fail closed unless the target account number is explicitly
  configured. Automatic "first account" discovery is not live authority.
- Position reconciliation collects every venue implicated by an open group or
  working ticket. Its comparison key is `execution_venue + OCC symbol`, so an
  identical option held at Public and Tastytrade cannot offset or hide a
  discrepancy at the other broker. If a required venue inventory is
  unavailable, repair is suspended and the venue is reported as unavailable.
- Tastytrade responses normalize working, partial-fill, fill-price, remaining
  quantity, and fill-time fields into the existing shared lifecycle contract.

Market data and execution remain deliberately separate. Public/shared quotes
may build and manage the strategy; Tastytrade supplies native dry-runs, order
receipts, status, and positions for its venue. DXLink is not yet a Kamandal
quote provider. Before the protected live flip, run the same two-leg open,
two-leg close, mixed adjustment, partial-fill, and cancel-replace contract
against a Tastytrade sandbox account, then perform a separately approved bounded
one-contract canary with live quotes. The sandbox is evidence for broker
plumbing, not evidence of fill quality or strategy economics.

See [TASTYTRADE_LIVE_HANDOFF.md](TASTYTRADE_LIVE_HANDOFF.md) for the secure
credential placement and staged broker-validation procedure.
