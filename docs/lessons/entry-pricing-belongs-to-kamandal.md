---
title: Entry pricing belongs to Kamandal across execution venues
type: bug
area: entry execution
date: 2026-09-10
tags: [pricing, tastytrade, preflight, replacement]
refs: [src/kamandal_v2/live/pricing.py, src/kamandal_v2/market/tastytrade.py, src/kamandal_v2/live/orders.py, src/kamandal_v2/live/execution.py]
---

# Entry pricing belongs to Kamandal across execution venues

Kamandal selects the contracts and freezes one economic price envelope at candidate
preflight. Adapters translate that envelope into broker price/side conventions and
validate the proposed price. They do not silently substitute their own midpoint.
The executor advances within the original envelope, preserving contracts, quantity,
venue, lifecycle identity, and approved risk budget. It never chooses another trade.

The September 10 NTAP strangle exposed a venue gap: Public applied the shared
pricing policy; Tastytrade used the raw midpoint and omitted `entry_pricing`.
Ticket construction recognized only Public's `limitPrice`, so a native price was
not portable either. Both replacement endpoints then defaulted to the current
price, producing two $5.40-to-$5.40 replacements before expiry.

Tastytrade now uses the existing shared policy and attaches its accepted signed
limit plus complete pricing metadata. Opening-ticket translation preserves both,
including the standalone CSA opening path. Normal campaign behavior remains:
favorable half-improvement, midpoint, then a concession capped by every configured
allowance and explicit economic bound. The deployed absolute allowance is $0.10
per share; this repair does not change policy configuration. Missing economic
bounds suppress the candidate before a broker dry-run. Broker rejection remains
blocking; do not guess a new tick or widen a bound to obtain acceptance.

Replacement requires a distinct price that moves toward execution without changing
credit/debit side. Missing metadata, missing endpoints, exhausted sequences, and
no-op or reverse-direction prices cannot call the broker. Legacy working tickets
are left under normal expiry/reconciliation rather than retrofitted with newly
invented economic authority. Fresh preflight and the strangle's approved BPR budget
still gate replacements. Uncertain outcomes and partial fills retain their existing
lineage/reconciliation owner.

Acceptance includes native Tastytrade dry-run -> unified exact-package planning ->
selected live ticket -> immutable replacement sequence, plus credit/debit parity,
metadata preservation across fresh preflight and CSA translation, no-broker-call
checks for invalid repricing, and a fresh BPR increase that blocks replacement.
Use `PYTHONPATH=.:src .venv/bin/python -m pytest -q -o addopts=''`.
