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
change. Before that flip, prove Tastytrade credentials and account selection,
one dry-run/preflight contract, broker-native signed multi-leg semantics,
receipt redaction, cancel/replace, partial fills, reconciliation, and a bounded
one-contract canary. No deployment or Sheet migration in this design implies
permission to submit real orders.
