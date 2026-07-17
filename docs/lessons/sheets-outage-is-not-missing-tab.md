---
title: A Sheets outage is not a missing tab
type: incident
area: Google Sheets integration
date: 2026-07-17
tags: [google-sheets, retries, fail-closed, reliability]
refs: [src/kamandal_v2/sheets.py:18, src/kamandal_v2/sheets.py:112, tests/test_sheets.py:21, pull/3]
---

# A Sheets Outage Is Not a Missing Tab

## Context

The live-approved-orders job failed while reading staged approvals with
`APIError: [503]: The service is currently unavailable.` The prior worksheet
lookup caught every exception and treated it as evidence that the tab did not
exist, which could turn a transient Google outage into an attempted
`add_worksheet` mutation.

## What We Learned

Remote lookup failure and resource absence are different states. A transient
rate limit, server error, or transport interruption should be retried within a
small bound. A worksheet should be created only when Google explicitly reports
`WorksheetNotFound`. Authentication, permission, configuration, and exhausted
transient failures must propagate so the trading job fails closed.

## Why / When It Applies

This applies anywhere Kamandal both discovers and creates remote state. A broad
exception around a read can accidentally authorize a write, obscure the real
failure, and create duplicates. The distinction matters most in scheduled live
jobs because a brief provider outage should neither mutate operator-owned state
nor suppress the safety alert if recovery is exhausted.

## Specifics

- Retry only 429, 500, 502, 503, 504, and transient transport failures
  (`src/kamandal_v2/sheets.py:18`).
- Bound retries to three attempts by default, with 1-second then 2-second
  backoff; expose the limits through configuration.
- Apply the same retry policy to spreadsheet open, worksheet lookup, reads,
  clears, updates, freezes, and creates.
- Catch only `WorksheetNotFound` before creating a tab
  (`src/kamandal_v2/sheets.py:112`).
- Test both sides of the boundary: a 503 lookup recovers without a create, while
  an explicit missing-tab response creates exactly once (`tests/test_sheets.py`).

## Apply It Next Time

Before adding create-on-read behavior, classify failures into explicit absence,
transient provider failure, and permanent/auth/configuration failure. Retry only
the transient class, create only for explicit absence, and let all other failures
surface unchanged. Add a test proving an outage cannot cross the mutation
boundary.

## Dead Ends

- Catching `Exception` and creating the resource hides outages and permission
  failures behind misleading duplicate-resource behavior.
- Retrying every error delays permanent failures and can repeat unsafe writes.
- Retrying forever makes the scheduler appear healthy while live decisions are
  stale; bounded recovery must still end in a visible fail-closed alert.
