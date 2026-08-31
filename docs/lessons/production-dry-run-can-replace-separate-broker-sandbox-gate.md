---
title: Production dry-run evidence can replace a separate broker sandbox gate
type: decision
area: tastytrade-live-promotion
date: 2026-08-30
tags: [tastytrade, sandbox, dry-run, strangle, promotion]
refs: [docs/TASTYTRADE_LIVE_HANDOFF.md, docs/STRANGLE_EXPERIMENT_STATUS.md, data/kamandal_v2.db]
---

# Production Dry-Run Evidence Can Replace a Separate Broker Sandbox Gate

## What We Learned

Do not require a second broker account merely because a certification sandbox
exists. When the deployed shadow path already authenticates to the intended
production account and records exact-leg broker dry-run BPR, the sandbox is optional
adapter research rather than a promotion gate.

## Context and Evidence

Oldmac short-strangle candidates from 2026-08-26 through 2026-08-28 recorded
`execution_venue=tasty_primary` and `bpr_source=tastytrade_dry_run`, including TLT
and IEF packages. `kamandal tastytrade-readiness` also reported the production
account, documented host, Orders API version, and multileg payload capabilities
configured. The certification sandbox would require separate credentials, uses
delayed quotes, resets trade state daily, and cannot prove production fills.

## When It Applies

Use the separate sandbox when an adapter change needs disposable submit/status/
cancel/replace experiments before any production canary. It becomes necessary if
production dry-run authentication or exact-leg BPR is absent, untrusted, or changed.

## Apply It Next Time

Ask what uncertainty the sandbox removes. For this lane, require fresh natural
shadow reachability, production dry-run BPR, account capacity, health, policy hash,
and reconciliation readiness. Then use one separately approved production canary to
prove the remaining broker state machine.

## Dead Ends

Do not confuse broker-inert readiness output with authenticated evidence. Conversely,
do not ignore authenticated natural dry-run receipts and force the operator to build
another account. Neither dry-run nor sandbox evidence grants live order authority.
