---
title: Multileg exits separate value, reason, and urgency before session gating
type: decision
area: lifecycle-management
date: 2026-08-23
tags: [options, midpoint, quote-quality, exits, sessions]
refs: [commit:5962f85, src/kamandal_v2/strategy_lanes/observations.py, src/kamandal_v2/strategy_lanes/action_arbiter.py, src/kamandal_v2/live/option_sessions.py, tests/test_package_observations.py]
---

# Multileg Exits Separate Value, Reason, And Urgency

## What We Learned

A multileg manager needs three separate facts before it can act: what the
package is worth, why an exit is due, and how urgently that reason may use the
current session. A broker-facing natural price cannot safely answer all three.

## Context and Evidence

The canonical manager once used bid/ask natural liquidation as P&L. A TLT leg
quoted `$0.15 x $2.70` therefore looked like an immediate large loss and the
shadow lifecycle closed at the ask-side package. A later NVDA loss close was
selected at 08:30 CT, when an adverse quote is least trustworthy.

Commit `5962f85` makes a validated package observation the shared decision
input. Midpoint is the value mark; natural is the execution boundary; the
frozen Sheet spread limit decides whether either is usable. The action retains
its reason class through the ticket. Profit and scheduled exits can use valid
opening/closing quotes, while adverse price exits require two valid normal-
window observations. Scheduled DTE/event intent ranks ahead of adverse loss so
the loss buffer cannot accidentally suppress a due exit.

## When It Applies

Use this contract for every multileg profit, loss, event, DTE, expiry, or
adjustment path in live and shadow. A true structural emergency may retain a
separate higher-priority path, but it still requires complete executable quote
evidence unless the broker itself forces the effect.

## Apply It Next Time

Trace one raw chain snapshot through observation, action arbitration, session
permission, typed ticket, reprice children, fill, and history. Test overlapping
reasons at 08:30 and the closing buffer. Assert that wide/stale/missing quotes
append evidence and preserve mandatory ownership without creating a price-
derived order; the existing health owner escalates only after safe automatic
retry stalls.

## Dead Ends

- Treating natural liquidation as both fair value and executable permission.
- Calling every loss threshold a hard emergency.
- Applying the loss session buffer after arbitration without first ensuring a
  scheduled exit has higher precedence.
- Updating only latest lifecycle metadata; repeated holds and invalid quotes
  are needed to explain the eventual trade outcome.
